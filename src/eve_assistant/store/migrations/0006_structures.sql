-- Player-owned structures seen in characters' locations and assets, looked up
-- through ESI (needs docking access). Names are chosen by the structure's
-- owner, so they are player-written text.
CREATE TABLE structures (
    structure_id    INTEGER PRIMARY KEY,
    accessible      INTEGER NOT NULL,   -- 0 when ESI refused the lookup
    player_name     TEXT,               -- player-written; display only
    solar_system_id INTEGER,
    type_id         INTEGER,
    owner_id        INTEGER,
    checked_at      TEXT NOT NULL,
    checked_by      INTEGER             -- character whose access was used
);
