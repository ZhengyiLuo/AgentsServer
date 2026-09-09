-- Explicitly mark new all-server inbox mail. Historical shared Bulletin rows
-- retain NULL and are never expanded or reclassified as mail.
ALTER TABLE team_messages ADD COLUMN destination TEXT
    CHECK(destination IS NULL OR destination = 'all_servers');
