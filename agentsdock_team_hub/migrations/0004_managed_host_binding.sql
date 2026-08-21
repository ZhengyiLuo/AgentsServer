-- Bind an embedded Hub database to exactly one stable AgentsServer identity.
-- Standalone Hubs remain unbound until their first managed adoption.

CREATE TABLE managed_host_bindings (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    hub_id TEXT NOT NULL UNIQUE REFERENCES hub_metadata(hub_id) ON DELETE RESTRICT,
    server_identity TEXT NOT NULL UNIQUE
        CHECK(length(trim(server_identity)) BETWEEN 8 AND 240),
    created_at INTEGER NOT NULL CHECK(created_at >= 0)
);

CREATE TRIGGER managed_host_binding_is_immutable_update
BEFORE UPDATE ON managed_host_bindings
BEGIN
    SELECT RAISE(ABORT, 'managed Hub host binding is immutable');
END;

CREATE TRIGGER managed_host_binding_is_immutable_delete
BEFORE DELETE ON managed_host_bindings
BEGIN
    SELECT RAISE(ABORT, 'managed Hub host binding is immutable');
END;
