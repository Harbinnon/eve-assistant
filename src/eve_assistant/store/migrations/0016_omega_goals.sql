-- A goal for Omega via PLEX (roadmap 3.5): its ISK target follows the PLEX
-- price, so it isn't fixed when the goal is set. SQLite can't widen a CHECK,
-- so the table is rebuilt with every goal copied across unchanged.
CREATE TABLE goals_new (
    goal_id      INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('fly_ship', 'skill', 'isk', 'omega')),
    character_id INTEGER REFERENCES characters(character_id),
    type_id      INTEGER,          -- ship or skill, for fly_ship and skill goals
    level        INTEGER,          -- skill level; months for omega goals
    isk          REAL,             -- for isk goals
    label        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'done', 'dropped')),
    status_at    TEXT
);
INSERT INTO goals_new (goal_id, kind, character_id, type_id, level, isk, label, created_at,
                       status, status_at)
SELECT goal_id, kind, character_id, type_id, level, isk, label, created_at, status, status_at
FROM goals;
DROP TABLE goals;
ALTER TABLE goals_new RENAME TO goals;
