-- Freelance Jobs from ESI's public listing that the planner can use (mining
-- and delivery). Public reference data, like market prices: kept current, not
-- append-only. Only what planning needs: never the creator or the description.
CREATE TABLE freelance_jobs (
    job_id           TEXT PRIMARY KEY,
    name             TEXT NOT NULL,      -- player-written: shown labelled
    method           TEXT NOT NULL,      -- MineOre, DeliverItem (others: kept to skip)
    state            TEXT NOT NULL,
    last_modified    TEXT,
    type_ids         TEXT NOT NULL,      -- JSON list: ore or item types the job wants
    locations        TEXT NOT NULL,      -- JSON {value_type: [ids]}; {} = anywhere
    reward_per_unit  REAL NOT NULL,
    unit_limit       INTEGER,            -- per participant; NULL = no limit
    progress_current INTEGER NOT NULL,
    progress_desired INTEGER NOT NULL,
    reward_remaining REAL NOT NULL,
    expires          TEXT,
    restricted       INTEGER NOT NULL,   -- ACL-protected: may not take us
    fetched_at       TEXT NOT NULL
);
