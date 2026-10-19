-- Milestone 6: corporations, nullsec and Planetary Interaction.

-- The corporations the owner's characters belong to (public, /corporations/{id}),
-- for corp tax and friendly sovereignty. The latest reading only.
CREATE TABLE corporations (
    corporation_id INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    ticker         TEXT,
    alliance_id    INTEGER,
    tax_rate       REAL,               -- NULL when ESI doesn't say (NPC corporations)
    fetched_at     TEXT NOT NULL
);

-- Who holds each system (public, /sovereignty/systems): IDs only, a map like
-- the stargates. The latest reading only.
CREATE TABLE sov_systems (
    system_id      INTEGER PRIMARY KEY,
    alliance_id    INTEGER,
    corporation_id INTEGER,
    faction_id     INTEGER,
    fetched_at     TEXT NOT NULL
) WITHOUT ROWID;

-- Static data for Planetary Interaction, filled by the static data loader (version 5).
CREATE TABLE sde_planets (
    planet_id       INTEGER PRIMARY KEY,
    system_id       INTEGER NOT NULL,
    celestial_index INTEGER,
    type_id         INTEGER
);

CREATE TABLE sde_planet_schematics (
    schematic_id  INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    cycle_seconds INTEGER NOT NULL
);

CREATE TABLE sde_planet_schematic_types (
    schematic_id INTEGER NOT NULL,
    type_id      INTEGER NOT NULL,
    quantity     INTEGER NOT NULL,
    is_input     INTEGER NOT NULL,     -- 1 for an input, 0 for the output
    PRIMARY KEY (schematic_id, type_id)
) WITHOUT ROWID;
