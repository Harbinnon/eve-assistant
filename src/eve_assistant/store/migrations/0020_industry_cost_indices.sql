-- Manufacturing cost index per system (roadmap 5.4), from ESI's public
-- /industry/systems, read once a day. The latest reading only.
CREATE TABLE industry_cost_indices (
    system_id     INTEGER PRIMARY KEY,
    manufacturing REAL NOT NULL,
    fetched_at    TEXT NOT NULL
) WITHOUT ROWID;
