-- A skill version's announcement may be hidden independently of the library.
-- Keep the original message, immutable versions, and bound attachments intact.
-- Poster authorization remains in HubStore; the journal retains its existing
-- same-team actor and append-only constraints.
DROP TRIGGER network_content_deletions_require_message;

CREATE TRIGGER network_content_deletions_require_message
BEFORE INSERT ON network_content_deletions
FOR EACH ROW WHEN NEW.resource_kind = 'message' AND NOT EXISTS (
    SELECT 1 FROM team_messages AS m
    WHERE m.team_id = NEW.team_id AND m.id = NEW.resource_id
      AND m.kind IN ('message', 'skill')
)
BEGIN
    SELECT RAISE(ABORT, 'team message deletion source is unavailable');
END;
