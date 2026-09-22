-- Metadata-only immutable Bulletin changes. No outbox polling or body copies.
CREATE TABLE team_bulletin_changes (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    team_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    change_kind TEXT NOT NULL CHECK(change_kind IN ('created','revised','deleted')),
    message_version INTEGER NOT NULL CHECK(message_version >= 1),
    FOREIGN KEY(team_id,message_id) REFERENCES team_messages(team_id,id) ON DELETE RESTRICT
);
CREATE INDEX team_bulletin_changes_by_team ON team_bulletin_changes(team_id,sequence);

-- Existing Bulletin establishes a baseline; clients never toast this snapshot.
INSERT INTO team_bulletin_changes(id,team_id,message_id,change_kind,message_version)
SELECT 'bchg_' || lower(hex(randomblob(16))), m.team_id, m.id, 'created',
       COALESCE((SELECT MAX(version) FROM team_message_revisions r
                 WHERE r.team_id=m.team_id AND r.message_id=m.id),1)
FROM team_messages m
WHERE EXISTS (SELECT 1 FROM team_message_recipients r
              WHERE r.team_id=m.team_id AND r.message_id=m.id AND r.recipient_kind='all')
ORDER BY m.queue_ordinal;

CREATE TRIGGER team_bulletin_created AFTER INSERT ON team_message_recipients
WHEN NEW.recipient_kind='all'
BEGIN
    INSERT INTO team_bulletin_changes(id,team_id,message_id,change_kind,message_version)
    VALUES ('bchg_' || lower(hex(randomblob(16))),NEW.team_id,NEW.message_id,'created',1);
END;
CREATE TRIGGER team_bulletin_revised AFTER INSERT ON team_message_revisions
WHEN EXISTS (SELECT 1 FROM team_message_recipients r
             WHERE r.team_id=NEW.team_id AND r.message_id=NEW.message_id AND r.recipient_kind='all')
BEGIN
    INSERT INTO team_bulletin_changes(id,team_id,message_id,change_kind,message_version)
    VALUES ('bchg_' || lower(hex(randomblob(16))),NEW.team_id,NEW.message_id,'revised',NEW.version);
END;
CREATE TRIGGER team_bulletin_deleted AFTER INSERT ON network_content_deletions
WHEN NEW.resource_kind='message' AND EXISTS (
    SELECT 1 FROM team_message_recipients r
    WHERE r.team_id=NEW.team_id AND r.message_id=NEW.resource_id AND r.recipient_kind='all')
BEGIN
    INSERT INTO team_bulletin_changes(id,team_id,message_id,change_kind,message_version)
    VALUES ('bchg_' || lower(hex(randomblob(16))),NEW.team_id,NEW.resource_id,'deleted',
        COALESCE((SELECT MAX(version) FROM team_message_revisions r
                  WHERE r.team_id=NEW.team_id AND r.message_id=NEW.resource_id),1));
END;
CREATE TRIGGER team_bulletin_changes_immutable BEFORE UPDATE ON team_bulletin_changes
BEGIN SELECT RAISE(ABORT,'Bulletin changes are immutable'); END;
CREATE TRIGGER team_bulletin_changes_retained BEFORE DELETE ON team_bulletin_changes
BEGIN SELECT RAISE(ABORT,'Bulletin changes are retained'); END;
