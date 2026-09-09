-- A server-created shared network has one internal service owner. Ordinary
-- services still cannot acquire human membership roles or own personal teams.
DROP TRIGGER memberships_require_compatible_principal;
DROP TRIGGER memberships_update_requires_compatible_principal;

CREATE TRIGGER memberships_require_compatible_principal
BEFORE INSERT ON memberships
FOR EACH ROW WHEN NOT (
    (NEW.role = 'automation' AND COALESCE(
        (SELECT kind FROM principals WHERE id = NEW.principal_id), ''
    ) = 'service')
    OR (NEW.role <> 'automation' AND COALESCE(
        (SELECT kind FROM principals WHERE id = NEW.principal_id), ''
    ) = 'human')
    OR (NEW.role = 'owner' AND NEW.principal_id = 'service_managed_network_owner'
        AND EXISTS (
            SELECT 1 FROM principals p JOIN service_accounts s ON s.principal_id=p.id
            JOIN teams t ON t.id=NEW.team_id
            JOIN managed_host_bindings b ON b.singleton=1
            WHERE p.id=NEW.principal_id AND p.kind='service' AND p.status='active'
              AND s.service_identifier='agentsdock.team-hub.managed-network-owner'
              AND t.kind='shared' AND t.created_by_principal_id=p.id
        ))
)
BEGIN
    SELECT RAISE(ABORT, 'membership role is incompatible with principal kind');
END;

CREATE TRIGGER memberships_update_requires_compatible_principal
BEFORE UPDATE OF principal_id, role ON memberships
FOR EACH ROW WHEN NOT (
    (NEW.role = 'automation' AND COALESCE(
        (SELECT kind FROM principals WHERE id = NEW.principal_id), ''
    ) = 'service')
    OR (NEW.role <> 'automation' AND COALESCE(
        (SELECT kind FROM principals WHERE id = NEW.principal_id), ''
    ) = 'human')
    OR (NEW.role = 'owner' AND NEW.principal_id = 'service_managed_network_owner'
        AND EXISTS (
            SELECT 1 FROM principals p JOIN service_accounts s ON s.principal_id=p.id
            JOIN teams t ON t.id=NEW.team_id
            JOIN managed_host_bindings b ON b.singleton=1
            WHERE p.id=NEW.principal_id AND p.kind='service' AND p.status='active'
              AND s.service_identifier='agentsdock.team-hub.managed-network-owner'
              AND t.kind='shared' AND t.created_by_principal_id=p.id
        ))
)
BEGIN
    SELECT RAISE(ABORT, 'membership role is incompatible with principal kind');
END;
