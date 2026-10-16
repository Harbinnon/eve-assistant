-- Static data needed for refining, routes and standings. Filled by the next
-- `eve-assistant sde-update --force` (or the next new SDE build).

CREATE TABLE sde_type_materials (
    type_id          INTEGER NOT NULL,   -- what is reprocessed (per portion)
    material_type_id INTEGER NOT NULL,
    quantity         INTEGER NOT NULL,
    PRIMARY KEY (type_id, material_type_id)
) WITHOUT ROWID;

-- One row per stargate jump direction.
CREATE TABLE sde_jumps (
    from_system_id INTEGER NOT NULL,
    to_system_id   INTEGER NOT NULL,
    PRIMARY KEY (from_system_id, to_system_id)
) WITHOUT ROWID;

CREATE TABLE sde_npc_corporations (
    corporation_id INTEGER PRIMARY KEY,
    faction_id     INTEGER
);

-- Bumped whenever the loader learns to fill new tables, so the tool can say
-- "reload the static data" instead of silently missing rows.
ALTER TABLE sde_meta ADD COLUMN loader_version INTEGER NOT NULL DEFAULT 1;
