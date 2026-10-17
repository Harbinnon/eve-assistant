-- Static data for milestone 5: what blueprints make (roadmap 5.4) and the
-- mission agents (5.3). Filled by the static data loader, like the others.

-- Manufacturing only: research, copying and invention aren't open to Alpha.
CREATE TABLE sde_blueprints (
    blueprint_type_id INTEGER PRIMARY KEY,
    product_type_id   INTEGER NOT NULL,
    quantity          INTEGER NOT NULL,   -- units made per run
    seconds           INTEGER NOT NULL,   -- per run, before skills and the blueprint's TE
    max_runs          INTEGER             -- most runs one copy can hold
) WITHOUT ROWID;
CREATE INDEX sde_blueprints_product ON sde_blueprints (product_type_id);

CREATE TABLE sde_blueprint_materials (
    blueprint_type_id INTEGER NOT NULL,
    material_type_id  INTEGER NOT NULL,
    quantity          INTEGER NOT NULL,   -- per run, before the blueprint's ME
    PRIMARY KEY (blueprint_type_id, material_type_id)
) WITHOUT ROWID;

CREATE TABLE sde_blueprint_skills (
    blueprint_type_id INTEGER NOT NULL,
    skill_id          INTEGER NOT NULL,
    level             INTEGER NOT NULL,
    PRIMARY KEY (blueprint_type_id, skill_id)
) WITHOUT ROWID;

-- NPC agents (CCP's characters, not players): where they are and what they offer.
CREATE TABLE sde_agents (
    agent_id       INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    corporation_id INTEGER NOT NULL,
    station_id     INTEGER,
    division_id    INTEGER NOT NULL,   -- 22 Distribution, 23 Mining, 24 Security
    level          INTEGER NOT NULL,
    agent_type_id  INTEGER NOT NULL,   -- 2 = basic agent
    is_locator     INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX sde_agents_station ON sde_agents (station_id);
