-- Military campaigns (roadmap 5.6). What each campaign and objective is and
-- pays comes from the static data; whether it's running comes from ESI's
-- public /military-campaigns, read once a day.
CREATE TABLE sde_campaigns (
    campaign_id TEXT PRIMARY KEY,
    title       TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE sde_campaign_objectives (
    objective_id        TEXT PRIMARY KEY,
    campaign_id         TEXT NOT NULL,
    title               TEXT NOT NULL,
    method              TEXT NOT NULL,   -- e.g. MineOre, CompleteAgentMission
    career_path         TEXT,
    isk                 REAL NOT NULL,   -- per progress interval
    lp                  REAL NOT NULL,
    lp_corporation_id   INTEGER,
    standing_percent    REAL NOT NULL,
    max_per_participant INTEGER,
    parameters          TEXT NOT NULL    -- JSON: where, which ore, which agents...
) WITHOUT ROWID;

-- The latest state of each campaign and objective, as ESI reports it.
CREATE TABLE campaign_states (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('campaign', 'objective')),
    state      TEXT NOT NULL,   -- e.g. Active, Completed, Expired
    progress   REAL,
    finished   TEXT,
    fetched_at TEXT NOT NULL
) WITHOUT ROWID;
