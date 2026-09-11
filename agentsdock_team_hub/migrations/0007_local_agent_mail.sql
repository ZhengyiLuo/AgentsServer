-- Permit the process-local Team Hub control principal to author passive mail
-- only as the immutable managed host node. It still cannot authenticate over
-- HTTP, claim another server, or bypass recipient/agent ownership triggers.

DROP TRIGGER network_mailbox_sender_is_authorized;

CREATE TRIGGER network_mailbox_sender_is_authorized
BEFORE INSERT ON network_mailbox_items
FOR EACH ROW WHEN NOT (
    (
        NEW.sender_kind = 'human'
        AND EXISTS (
            SELECT 1
            FROM principals AS p
            JOIN memberships AS m
              ON m.team_id = NEW.team_id AND m.principal_id = p.id
            WHERE p.id = NEW.sender_principal_id
              AND p.kind = 'human'
              AND p.status = 'active'
              AND m.status = 'active'
        )
    )
    OR (
        NEW.sender_kind IN ('server', 'agent')
        AND (
            EXISTS (
                SELECT 1
                FROM network_peer_bindings AS b
                JOIN nodes AS n
                  ON n.team_id = b.team_id AND n.id = b.node_id
                WHERE b.team_id = NEW.team_id
                  AND b.node_id = NEW.sender_node_id
                  AND b.service_principal_id = NEW.sender_principal_id
                  AND b.status = 'active'
                  AND n.status = 'active'
            )
            OR EXISTS (
                SELECT 1
                FROM managed_host_bindings AS managed
                JOIN nodes AS n
                  ON n.team_id = NEW.team_id
                 AND n.id = NEW.sender_node_id
                 AND n.server_identity = managed.server_identity
                JOIN principals AS p ON p.id = NEW.sender_principal_id
                JOIN service_accounts AS s ON s.principal_id = p.id
                JOIN memberships AS m
                  ON m.team_id = NEW.team_id
                 AND m.principal_id = p.id
                WHERE managed.singleton = 1
                  AND NEW.sender_principal_id = 'service_local_control'
                  AND n.status = 'active'
                  AND p.kind = 'service'
                  AND p.status = 'active'
                  AND s.service_identifier = 'agentsdock.team-hub.local-control'
                  AND m.role = 'automation'
                  AND m.status = 'active'
            )
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'network mailbox sender is not authorized');
END;
