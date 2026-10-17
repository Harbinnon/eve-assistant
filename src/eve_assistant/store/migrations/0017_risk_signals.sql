-- Live risk signals (roadmap 4.1), from ESI's public, aggregate data: no
-- player is named in any of it. Each table holds the latest reading only.

-- Ships, pods and NPCs destroyed per system in the last hour (/universe/system_kills).
CREATE TABLE system_kills (
    system_id   INTEGER PRIMARY KEY,
    ship_kills  INTEGER NOT NULL,
    pod_kills   INTEGER NOT NULL,
    npc_kills   INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL
) WITHOUT ROWID;

-- Systems in an active incursion (/incursions).
CREATE TABLE incursion_systems (
    system_id   INTEGER PRIMARY KEY,
    state       TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
) WITHOUT ROWID;

-- What insuring a hull costs and pays, per level (/insurance/prices).
CREATE TABLE insurance_prices (
    type_id     INTEGER NOT NULL,
    level       TEXT NOT NULL,
    cost        REAL NOT NULL,
    payout      REAL NOT NULL,
    PRIMARY KEY (type_id, level)
) WITHOUT ROWID;
